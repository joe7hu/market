"""Agent-related config helpers split out of core.config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ThesisMonitorAgentConfig:
    enabled: bool = False
    provider: str = "codex"
    model: str = "gpt-5.6-luna"
    reasoning_effort: str = "high"
    prompt_version: str = "thesis_v3_20260725"
    concurrency: int = 2
    evidence_items_per_symbol: int = 12
    preopen_enabled: bool = False
    material_event_enabled: bool = False
    debounce_minutes: int = 30
    max_material_runs_per_symbol_per_day: int = 2
    continuous_enabled: bool = False
    continuous_cadence_minutes: int = 120
    continuous_budget_usd: float = 5.0
    authority: str = "research_ranking_only"


def thesis_monitor_agent_config(raw: dict[str, Any]) -> ThesisMonitorAgentConfig:
    return ThesisMonitorAgentConfig(
        enabled=bool(raw.get("enabled", False)),
        provider=str(raw.get("provider", "codex")),
        model=str(raw.get("model", "gpt-5.6-luna")),
        reasoning_effort=str(raw.get("reasoning_effort", "high")),
        prompt_version=str(raw.get("prompt_version", "thesis_v3_20260725")),
        concurrency=int(raw.get("concurrency", 2)),
        evidence_items_per_symbol=int(raw.get("evidence_items_per_symbol", 12)),
        preopen_enabled=bool(raw.get("preopen_enabled", False)),
        material_event_enabled=bool(raw.get("material_event_enabled", False)),
        debounce_minutes=int(raw.get("debounce_minutes", 30)),
        max_material_runs_per_symbol_per_day=int(raw.get("max_material_runs_per_symbol_per_day", 2)),
        continuous_enabled=bool(raw.get("continuous_enabled", False)),
        continuous_cadence_minutes=int(raw.get("continuous_cadence_minutes", 120)),
        continuous_budget_usd=float(raw.get("continuous_budget_usd", 5.0)),
        authority=str(raw.get("authority", "research_ranking_only")),
    )


def thesis_monitor_agent_dict(config: ThesisMonitorAgentConfig) -> dict[str, Any]:
    return {
        "enabled": config.enabled,
        "provider": config.provider,
        "model": config.model,
        "reasoning_effort": config.reasoning_effort,
        "prompt_version": config.prompt_version,
        "concurrency": config.concurrency,
        "evidence_items_per_symbol": config.evidence_items_per_symbol,
        "preopen_enabled": config.preopen_enabled,
        "material_event_enabled": config.material_event_enabled,
        "debounce_minutes": config.debounce_minutes,
        "max_material_runs_per_symbol_per_day": config.max_material_runs_per_symbol_per_day,
        "continuous_enabled": config.continuous_enabled,
        "continuous_cadence_minutes": config.continuous_cadence_minutes,
        "continuous_budget_usd": config.continuous_budget_usd,
        "authority": config.authority,
    }
