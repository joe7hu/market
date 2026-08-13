"""Small, explicit config views used by web-facing configuration payloads."""

from __future__ import annotations

from typing import Any

from investment_panel.core.agent_providers import provider_catalog


def options_decision_system_dict(raw: Any) -> dict[str, Any]:
    """Return the safe decision-system settings exposed through ``/api/settings``."""

    return {
        "mode": raw.mode,
        "options_paper_actions_enabled": raw.options_paper_actions_enabled,
        "radar_paper_actions_enabled": raw.radar_paper_actions_enabled,
        "qqq_paper_actions_enabled": raw.qqq_paper_actions_enabled,
        "recovery_paper_actions_enabled": raw.recovery_paper_actions_enabled,
        "decision_inbox_enabled": raw.decision_inbox_enabled,
        "telegram_notifications_enabled": raw.telegram_notifications_enabled,
        "telegram_notifications_dry_run": raw.telegram_notifications_dry_run,
        "options_risk_sleeve_capital": raw.options_risk_sleeve_capital,
        "max_risk_per_trade_pct": raw.max_risk_per_trade_pct,
        "max_open_risk_pct": raw.max_open_risk_pct,
        "max_symbol_risk_pct": raw.max_symbol_risk_pct,
        "daily_loss_halt_pct": raw.daily_loss_halt_pct,
        "max_recovery_open_positions": raw.max_recovery_open_positions,
        "strategy_auto_promotion_enabled": raw.strategy_auto_promotion_enabled,
        "event_agent_debounce_minutes": raw.event_agent_debounce_minutes,
        "event_agent_max_batches_per_symbol_per_day": raw.event_agent_max_batches_per_symbol_per_day,
        "event_agent_max_tasks_per_batch": raw.event_agent_max_tasks_per_batch,
    }


def option_agent_config_dict(raw: Any) -> dict[str, Any]:
    """Return the option-agent settings without coupling config parsing to its view."""

    return {
        "enabled": raw.enabled,
        "command": raw.command,
        "timeout_seconds": raw.timeout_seconds,
        "thesis_limit": raw.thesis_limit,
        "postmortem_limit": raw.postmortem_limit,
        "provider": raw.provider,
        "model": raw.model,
        "reasoning_effort": raw.reasoning_effort,
        "auto_run_seconds": raw.auto_run_seconds,
        "max_runs_per_day": raw.max_runs_per_day,
        "experiment_enabled": raw.experiment_enabled,
        "experiment_auto_run_seconds": raw.experiment_auto_run_seconds,
        "context_sources": dict(raw.context_sources),
        "command_managed_by_provider": True,
        "provider_catalog": provider_catalog(),
    }
