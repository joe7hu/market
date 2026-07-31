"""Options-radar application actions behind the HTTP transport seam."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from investment_panel.database.actions import ActionRepository
from investment_panel.database.agents import AgentRepository
from investment_panel.database.analysis import AnalysisRepository
from investment_panel.database.authority import runtime_for_config
from investment_panel.database.options_analysis import refresh_options_radar
from investment_panel.database.options_history import OptionHistoryRepository
from investment_panel.database.options_decision_system import OptionsDecisionSystemRepository
from investment_panel.database.options_history_policy import OptionHistoryPolicyRepository, PolicyConflict
from investment_panel.core.robinhood_options.auth import load_robinhood_access_token
from investment_panel.core.robinhood_options.collector import RobinhoodMcpClient
from investment_panel.core.option_trade_ticket import build_option_trade_ticket, calibrated_cohort_ready
from investment_panel.core.decision import is_market_open
from investment_panel.database.options_risk_context import option_risk_contexts
from investment_panel.database.option_ticket_read import revalidate_published_tickets


def _decision_mode(config: Any) -> str:
    """Read the mode from either the web dict config or typed job config."""

    if isinstance(config, dict):
        return str((config.get("analysis") or {}).get("options_decision_system", {}).get("mode", "shadow"))
    return str(getattr(getattr(getattr(config, "analysis", None), "options_decision_system", None), "mode", "shadow"))


def _paper_actions_enabled(config: Any) -> bool:
    """Read the paper-action kill switch from web dict or typed job config."""

    if isinstance(config, dict):
        raw = (config.get("analysis") or {}).get("options_decision_system", {})
        return bool(raw.get("options_paper_actions_enabled", False))
    raw = getattr(getattr(getattr(config, "analysis", None), "options_decision_system", None), "options_paper_actions_enabled", False)
    return bool(raw)


def _options_risk_sleeve_capital(config: Any) -> float | None:
    if isinstance(config, dict):
        raw = (config.get("analysis") or {}).get("options_decision_system", {})
        value = raw.get("options_risk_sleeve_capital")
    else:
        raw = getattr(getattr(getattr(config, "analysis", None), "options_decision_system", None), "options_risk_sleeve_capital", None)
        value = raw
    return float(value) if value is not None and float(value) > 0 else None


def _robinhood_config(config: Any) -> Any:
    """Return Robinhood config from dict-backed web config or typed config."""

    if isinstance(config, dict):
        return (((config.get("data_sources") or {}).get("brokers") or {}).get("robinhood") or {})
    return getattr(getattr(getattr(config, "data_sources", None), "brokers", None), "robinhood", None)


def _cfg_get(raw: Any, key: str, default: Any = None) -> Any:
    if isinstance(raw, dict):
        return raw.get(key, default)
    return getattr(raw, key, default)


class OptionsActions:
    def __init__(self, config: Any) -> None:
        self.config = config
        self.runtime = runtime_for_config(config)
        self.actions = ActionRepository(self.runtime)
        self.agents = AgentRepository(self.runtime)
        self.analysis = AnalysisRepository(self.runtime)
        self.history = OptionHistoryRepository(
            self.runtime,
            options_risk_sleeve_capital=_options_risk_sleeve_capital(config),
        )
        self.policy = OptionHistoryPolicyRepository(self.runtime)
        mode = _decision_mode(config)
        self.decision_system = OptionsDecisionSystemRepository(self.runtime, mode=mode)

    def history_symbols(self) -> dict[str, Any]:
        return self.policy.symbols()

    def set_history_requested_state(self, symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "options_history_policy": self.policy.set_requested_state(
                symbol,
                requested_state=str(payload.get("requested_state") or ""),
                lock_version=int(payload.get("lock_version") or 0),
            )
        }

    @staticmethod
    def is_policy_conflict(exc: Exception) -> bool:
        return isinstance(exc, PolicyConflict)

    def history_snapshots(self, **filters: Any) -> dict[str, Any]:
        return self.history.snapshots(**filters)

    def history_chain(self, **filters: Any) -> dict[str, Any]:
        return self.history.chain(**filters)

    def history_surface(self, **filters: Any) -> dict[str, Any]:
        return self.history.surface(**filters)

    def history_surface_groups(self, **filters: Any) -> dict[str, Any]:
        return self.history.surface_groups(**filters)

    def history_surface_grid(self, **filters: Any) -> dict[str, Any]:
        return self.history.surface_grid(**filters)

    def history_legacy_surface(self, **filters: Any) -> dict[str, Any]:
        return self.history.legacy_surface(**filters)

    def history_curves(self, **filters: Any) -> dict[str, Any]:
        return self.history.curves(**filters)

    def history_anomalies(self, **filters: Any) -> dict[str, Any]:
        return self.history.anomalies(**filters)

    def history_health(self, *, symbol: str | None = None) -> dict[str, Any]:
        result = self.history.health(symbol=symbol)
        result["mode"] = _decision_mode(self.config)
        return result

    def decision_brief(self, **filters: Any) -> dict[str, Any]:
        payload = self.decision_system.decision_brief(**filters)
        if payload.get("strongest_candidate"):
            current_candidate = self._with_ticket(
                dict(payload["strongest_candidate"]),
                symbol=str(payload.get("symbol") or filters.get("symbol") or "QQQ"),
                evaluated_at=payload.get("as_of"),
            )
            payload["strongest_candidate"] = current_candidate
            payload["state"] = current_candidate["paper_state"]
            payload["summary"] = {
                **dict(payload.get("summary") or {}),
                "current_ticket_state": current_candidate["ticket"]["state"],
                "current_required_next_action": current_candidate["ticket"]["required_next_action"],
            }
        return payload

    def workspace(self, **filters: Any) -> dict[str, Any]:
        payload = self.decision_system.workspace(**filters)
        payload["paper_action_capability"]["enabled"] = _decision_mode(self.config) == "paper" and _paper_actions_enabled(self.config)
        payload["paper_action_capability"]["reason"] = (
            "enabled"
            if payload["paper_action_capability"]["enabled"]
            else "options_paper_actions_enabled_false"
        )
        return payload

    def candidates(self, **filters: Any) -> dict[str, Any]:
        payload = self.decision_system.candidates(**filters)
        symbol = str(filters.get("symbol") or "QQQ")
        payload["items"] = [self._with_ticket(dict(row), symbol=symbol, evaluated_at=payload.get("as_of")) for row in payload["items"]]
        payload["rows"] = payload["items"]
        return payload

    def relative_values(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.relative_values(**filters)

    def paper_journal(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.paper_journal(**filters)

    def shadow_observations(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.shadow_observations(**filters)

    def learning_progress(self, **filters: Any) -> dict[str, Any]:
        return self.decision_system.learning_progress(**filters)

    def verify_static_arbitrage(self, candidate_id: int) -> dict[str, Any]:
        robinhood = _robinhood_config(self.config)
        if robinhood is None or not bool(_cfg_get(robinhood, "enabled", False)):
            return self.decision_system.verification_result(candidate_id)
        client = RobinhoodMcpClient(
            str(_cfg_get(robinhood, "mcp_url", "https://agent.robinhood.com/mcp/trading")),
            auth_token=load_robinhood_access_token(robinhood),
            timeout_seconds=min(10, int(_cfg_get(robinhood, "timeout_seconds", 10))),
            max_response_bytes=int(_cfg_get(robinhood, "max_response_bytes", 8 * 1024 * 1024)),
        )
        return self.decision_system.verification_result(candidate_id, client)

    def signal_detail(self, decision_id: UUID) -> dict[str, Any] | None:
        detail = self.analysis.option_signal_detail(decision_id)
        if detail is None:
            return None
        return revalidate_published_tickets(
            self.runtime,
            [detail],
            sleeve_capital=_options_risk_sleeve_capital(self.config),
        )[0]

    def stage_paper_entry(self, decision_id: UUID, payload: dict[str, Any]) -> dict[str, Any]:
        mode = _decision_mode(self.config)
        if mode != "paper":
            raise ValueError("options decision system is in shadow mode; paper entry is disabled")
        if not _paper_actions_enabled(self.config):
            raise ValueError("options paper actions kill switch is disabled")
        return self.actions.stage_option_paper_entry(
            decision_id=decision_id,
            idempotency_key=payload.get("idempotency_key"),
            ticket_version=payload.get("ticket_version"),
            quantity=payload.get("quantity"),
            limit_price=payload.get("limit_price"),
            current_options_risk_sleeve_capital=_options_risk_sleeve_capital(self.config),
        )

    def _with_ticket(self, candidate: dict[str, Any], *, symbol: str, evaluated_at: Any) -> dict[str, Any]:
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
            sleeve_capital=_options_risk_sleeve_capital(self.config),
            **risk_context,
            thesis=thesis,
            forecast=forecast,
            provenance={
                "analysis_evaluated_at": evaluated_at,
                "revisions": {"model": candidate.get("model_version")},
            },
        )
        candidate["execution_ready"] = candidate["ticket"]["state"] == "READY"
        if not candidate["execution_ready"] and candidate.get("paper_state") == "PAPER_READY":
            candidate["paper_state"] = "WATCH"
        candidate["blockers"] = list(candidate["ticket"]["blockers"])
        return candidate

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
            radar_refresh = refresh_options_radar(
                self.runtime,
                code_version="strategy-promotion",
                options_risk_sleeve_capital=_options_risk_sleeve_capital(self.config),
            )
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
