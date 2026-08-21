"""Canonical operational policy for Market refresh jobs.

This module owns the stable job identity and the policy callers otherwise have
to repeat: scheduler cadence, freshness expectations, subprocess timeouts,
first-run delay, and source-to-refresh routing.  The scheduler, launcher,
source catalog, and source-health query are adapters over this policy.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Any, Literal, Mapping


STAGGER_SECONDS = 5.0
_TRUTHY_OFF = {"0", "false", "off", "no"}


@dataclass(frozen=True)
class JobDefinition:
    name: str
    timeout_seconds: int | None = None
    freshness_seconds: int | None = None
    initial_delay: Literal["stagger", "one_interval"] = "stagger"


def _job(
    name: str,
    *,
    timeout_seconds: int | None = None,
    freshness_seconds: int | None = None,
    initial_delay: Literal["stagger", "one_interval"] = "stagger",
) -> JobDefinition:
    return JobDefinition(name, timeout_seconds, freshness_seconds, initial_delay)


JOB_DEFINITIONS: dict[str, JobDefinition] = {
    definition.name: definition
    for definition in (
        _job("full_market_refresh"),
        _job("daily_screen"),
        _job("refresh_decision_models"),
        _job("update_preopen_daily_brief_scheduled"),
        _job("hourly_options_radar"),
        _job("premarket_options_intelligence"),
        _job("update_ibkr_options", freshness_seconds=3600, initial_delay="one_interval"),
        _job("update_robinhood_options", freshness_seconds=259200, initial_delay="one_interval"),
        _job("robinhood_option_history", timeout_seconds=840, freshness_seconds=900, initial_delay="one_interval"),
        _job("detect_option_events", timeout_seconds=90, freshness_seconds=300),
        _job("refresh_options_radar"),
        _job("refresh_options_radar_deterministic"),
        _job("refresh_options_radar_signal"),
        _job("refresh_options_radar_signal_ibkr"),
        _job("refresh_options_radar_signal_robinhood"),
        _job(
            "options_radar_hard_refresh",
            timeout_seconds=5400,
            freshness_seconds=259200,
            initial_delay="one_interval",
        ),
        _job("refresh_options_radar_learning_marks"),
        _job("run_option_agents"),
        _job("run_agent_experiment", timeout_seconds=1_200),
        _job("run_option_recovery_agents", timeout_seconds=600),
        _job("process_options_paper_orders", timeout_seconds=60),
        _job("sync_decision_inbox", timeout_seconds=30),
        _job("refresh_symbol_decision_outcomes", timeout_seconds=300),
        _job("run_option_agents_force"),
        _job("run_option_agents_ondemand"),
        _job("run_thesis_monitor", timeout_seconds=1800),
        _job("run_thesis_monitor_force", timeout_seconds=1800),
        _job("run_thesis_monitor_preflight", timeout_seconds=1800),
        _job("update_broker_sources", freshness_seconds=3600),
        _job("update_market_data", freshness_seconds=86400),
        _job("update_free_sources", freshness_seconds=86400),
        _job("update_free_sources_radar", initial_delay="one_interval"),
        _job("update_research_sources", freshness_seconds=3600),
        _job("update_social_sources", freshness_seconds=1800),
        _job("update_event_calendar", freshness_seconds=86400),
        _job("update_disclosures", freshness_seconds=86400),
        _job("update_arco_data", freshness_seconds=14400),
        _job("postgres_retention"),
        _job("snapshot_database"),
    )
}


def job_definition(name: str) -> JobDefinition:
    try:
        return JOB_DEFINITIONS[name]
    except KeyError as exc:
        raise ValueError(f"unknown refresh job: {name}") from exc


def default_job_timeouts() -> dict[str, int]:
    return {
        name: definition.timeout_seconds
        for name, definition in JOB_DEFINITIONS.items()
        if definition.timeout_seconds is not None
    }


def job_timeout_seconds(name: str, overrides: Mapping[str, int] | None = None) -> int | None:
    env_key = f"MARKET_REFRESH_JOB_TIMEOUT_{name.upper()}"
    raw = os.environ.get(env_key)
    if raw is not None:
        try:
            value = int(raw.strip())
            return value if value > 0 else None
        except ValueError:
            pass
    if overrides is not None and name in overrides:
        return int(overrides[name])
    definition = JOB_DEFINITIONS.get(name)
    return definition.timeout_seconds if definition else None


def initial_delay_seconds(
    name: str,
    interval: int,
    offset: int,
    *,
    stagger_seconds: float = STAGGER_SECONDS,
) -> float:
    definition = JOB_DEFINITIONS.get(name)
    if definition and definition.initial_delay == "one_interval":
        return float(interval)
    return float(offset * stagger_seconds)


def scheduler_enabled() -> bool:
    return os.environ.get("MARKET_SCHEDULER_ENABLED", "1").strip().lower() not in _TRUTHY_OFF


def heavy_refresh_enabled() -> bool:
    return os.environ.get("MARKET_IN_PROCESS_HEAVY_REFRESH", "0").strip().lower() not in _TRUTHY_OFF


def scheduler_intervals(config: Any | None = None) -> dict[str, int]:
    option_source = os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood").strip().lower()
    if option_source == "ibkr":
        signal_job, source_job = "refresh_options_radar_signal_ibkr", "update_ibkr_options"
    else:
        option_source = "robinhood"
        signal_job, source_job = "refresh_options_radar_signal_robinhood", "update_robinhood_options"

    heavy_refresh = heavy_refresh_enabled()
    intervals: dict[str, int] = {}
    history_seconds = _env_int_optional("MARKET_OPTION_HISTORY_REFRESH_SECONDS")
    history_seconds = 900 if history_seconds is None else history_seconds
    if history_seconds > 0:
        intervals["robinhood_option_history"] = history_seconds
    event_detect_seconds = _env_int_optional("MARKET_OPTION_EVENT_DETECT_SECONDS")
    event_detect_seconds = 300 if event_detect_seconds is None else event_detect_seconds
    if event_detect_seconds > 0:
        intervals["detect_option_events"] = event_detect_seconds
    if option_source == "robinhood":
        hard_seconds = _env_int_optional("MARKET_OPTIONS_RADAR_HARD_REFRESH_SECONDS")
        if hard_seconds and hard_seconds > 0:
            intervals["options_radar_hard_refresh"] = hard_seconds
    else:
        radar_seconds = _env_int_optional("MARKET_RADAR_REFRESH_SECONDS")
        radar_seconds = 900 if radar_seconds is None else radar_seconds
        if radar_seconds > 0:
            intervals[signal_job] = radar_seconds
        source_seconds = _env_int_optional("MARKET_SOURCE_REFRESH_SECONDS")
        source_seconds = 3600 if source_seconds is None else source_seconds
        if source_seconds > 0:
            intervals[source_job] = source_seconds
    if option_source == "robinhood":
        radar_seconds = _env_int_optional("MARKET_RADAR_REFRESH_SECONDS")
        if radar_seconds and radar_seconds > 0:
            intervals[signal_job] = radar_seconds
        source_seconds = _env_int_optional("MARKET_SOURCE_REFRESH_SECONDS")
        if source_seconds and source_seconds > 0:
            intervals[source_job] = source_seconds

    learning_mark_seconds = _env_int_optional("MARKET_LEARNING_MARK_REFRESH_SECONDS")
    if learning_mark_seconds and learning_mark_seconds > 0:
        intervals["refresh_options_radar_learning_marks"] = learning_mark_seconds
    learning_seconds = _env_int_optional("MARKET_LEARNING_REFRESH_SECONDS")
    learning_seconds = (21600 if heavy_refresh else 0) if learning_seconds is None else learning_seconds
    if learning_seconds > 0:
        intervals["refresh_options_radar_deterministic"] = learning_seconds

    agent_seconds = _env_int_optional("MARKET_AGENT_REFRESH_SECONDS")
    agent_seconds = (86400 if heavy_refresh else 0) if agent_seconds is None else agent_seconds
    auto_run_enabled = True
    try:
        option_agent = _option_agent_config(config)
        auto_run_enabled = bool(_config_value(option_agent, "enabled", True))
        configured = int(_config_value(option_agent, "auto_run_seconds", 0) or 0)
        if configured > 0:
            agent_seconds = configured
    except Exception:
        pass
    if auto_run_enabled and agent_seconds > 0:
        intervals["run_option_agents"] = agent_seconds
    experiment_enabled = False
    experiment_seconds = 0
    try:
        experiment_enabled = bool(_config_value(option_agent, "experiment_enabled", False))
        experiment_seconds = int(_config_value(option_agent, "experiment_auto_run_seconds", 86_400) or 0)
    except Exception:
        pass
    if experiment_enabled and experiment_seconds > 0:
        intervals["run_agent_experiment"] = experiment_seconds

    recovery_agent_seconds = _env_int_optional("MARKET_RECOVERY_EVENT_AGENT_REFRESH_SECONDS")
    recovery_agent_seconds = 300 if recovery_agent_seconds is None else recovery_agent_seconds
    if auto_run_enabled and recovery_agent_seconds > 0:
        intervals["run_option_recovery_agents"] = recovery_agent_seconds

    decision_settings = _options_decision_config(config)
    inbox_seconds = _env_int("MARKET_DECISION_INBOX_REFRESH_SECONDS", 15, allow_zero=True)
    if bool(_config_value(decision_settings, "decision_inbox_enabled", True)) and inbox_seconds > 0:
        intervals["sync_decision_inbox"] = inbox_seconds
    paper_seconds = _env_int("MARKET_OPTIONS_PAPER_EXECUTION_SECONDS", 15, allow_zero=True)
    # Keep the deterministic manager alive even when all entry gates are off.
    # It stages nothing in that state, but it can still close existing paper
    # positions safely after a kill switch is used.
    if paper_seconds > 0:
        intervals["process_options_paper_orders"] = paper_seconds
    stock_outcome_seconds = _env_int("MARKET_SYMBOL_OUTCOME_REFRESH_SECONDS", 3600, allow_zero=True)
    if stock_outcome_seconds > 0:
        intervals["refresh_symbol_decision_outcomes"] = stock_outcome_seconds

    for job, env_name, default in (
        ("update_social_sources", "MARKET_SOCIAL_REFRESH_SECONDS", 1800),
        ("update_research_sources", "MARKET_RESEARCH_REFRESH_SECONDS", 3600),
        ("update_arco_data", "MARKET_ARCO_REFRESH_SECONDS", 14400),
        ("update_market_data", "MARKET_MARKET_DATA_REFRESH_SECONDS", 3600),
        ("update_preopen_daily_brief_scheduled", "MARKET_PREOPEN_BRIEF_REFRESH_SECONDS", 0),
    ):
        seconds = _env_int(env_name, default, allow_zero=True)
        if seconds > 0:
            intervals[job] = seconds
    return intervals


def scheduler_status(config: Any | None = None) -> dict[str, Any]:
    intervals = scheduler_intervals(config)
    option_source = os.environ.get("MARKET_RADAR_OPTION_SOURCE", "robinhood").strip().lower()
    return {
        "enabled": os.environ.get("MARKET_SCHEDULER_ENABLED", "1"),
        "heavy_refresh_enabled": "1" if heavy_refresh_enabled() else "0",
        "jobs": intervals,
        "agent_refresh_seconds": str(intervals.get("run_option_agents", 0)),
        "recovery_event_agent_refresh_seconds": str(intervals.get("run_option_recovery_agents", 0)),
        "radar_refresh_seconds": str(_first_interval(intervals, "options_radar_hard_refresh", "refresh_options_radar_signal")),
        "source_refresh_seconds": str(_first_interval(intervals, "options_radar_hard_refresh", "update_free_sources_radar", "update_ibkr_options", "update_robinhood_options")),
        "options_hard_refresh_seconds": str(intervals.get("options_radar_hard_refresh", 0)),
        "learning_mark_refresh_seconds": str(intervals.get("refresh_options_radar_learning_marks", 0)),
        "learning_refresh_seconds": str(intervals.get("refresh_options_radar_deterministic", 0)),
        "social_refresh_seconds": str(intervals.get("update_social_sources", 0)),
        "research_refresh_seconds": str(intervals.get("update_research_sources", 0)),
        "arco_refresh_seconds": str(intervals.get("update_arco_data", 0)),
        "market_data_refresh_seconds": str(intervals.get("update_market_data", 0)),
        "market_environment_refresh_seconds": "0",
        "preopen_brief_refresh_seconds": str(intervals.get("update_preopen_daily_brief_scheduled", 0)),
        "decision_inbox_refresh_seconds": str(intervals.get("sync_decision_inbox", 0)),
        "options_paper_execution_seconds": str(intervals.get("process_options_paper_orders", 0)),
        "symbol_outcome_refresh_seconds": str(intervals.get("refresh_symbol_decision_outcomes", 0)),
        "radar_option_source": option_source,
        "external_jobs": {
            "premarket_options_intelligence": {
                "owner": "launchd",
                "schedule": "weekdays 08:15 America/New_York",
                "market_calendar_gated": True,
                "includes": ["option_agent", "thesis_monitor", "preopen_narrative"],
            }
        },
    }


def source_refresh_job_names() -> set[str]:
    return {
        "update_broker_sources",
        "update_ibkr_options",
        "options_radar_hard_refresh",
        "update_social_sources",
        "update_arco_data",
        "update_research_sources",
        "update_event_calendar",
        "update_disclosures",
        "update_market_data",
    }


def source_refresh_jobs_sql() -> str:
    return """CASE
        WHEN source.family = 'legacy' OR source.id LIKE 'legacy-%' THEN ARRAY[]::text[]
        WHEN source.id = 'ibkr' THEN ARRAY['update_broker_sources', 'update_ibkr_options']::text[]
        WHEN source.id = 'moomoo' THEN ARRAY['update_broker_sources']::text[]
        WHEN source.id = 'robinhood' THEN ARRAY['options_radar_hard_refresh']::text[]
        WHEN source.id = 'birdclaw_primary_tweets' THEN ARRAY['update_social_sources']::text[]
        WHEN source.id = 'arco' THEN ARRAY['update_arco_data']::text[]
        WHEN source.id LIKE 'news_%' OR source.id LIKE 'blog_%' THEN ARRAY['update_research_sources']::text[]
        WHEN source.id = 'official-event-calendar' THEN ARRAY['update_event_calendar']::text[]
        WHEN source.id LIKE 'house_%' OR source.id LIKE 'sec_13f_%'
          OR source.id LIKE 'disclosure_csv_%' OR source.id = 'sec_disclosures'
          THEN ARRAY['update_disclosures']::text[]
        WHEN source.id IN ('watchlist_quote', 'daily-market-prices', 'tradingview',
                           'yfinance_info', 'coingecko', 'yfinance')
          THEN ARRAY['update_market_data']::text[]
        ELSE ARRAY[]::text[] END"""


def source_primary_refresh_job_sql() -> str:
    return """CASE
        WHEN source.family = 'legacy' OR source.id LIKE 'legacy-%' THEN NULL
        WHEN source.id = 'robinhood' THEN 'options_radar_hard_refresh'
        WHEN source.id = 'ibkr' AND worst.capability = 'option_quotes' THEN 'update_ibkr_options'
        WHEN source.id IN ('ibkr', 'moomoo') THEN 'update_broker_sources'
        WHEN source.id = 'birdclaw_primary_tweets' THEN 'update_social_sources'
        WHEN source.id = 'arco' THEN 'update_arco_data'
        WHEN source.id LIKE 'news_%' OR source.id LIKE 'blog_%' THEN 'update_research_sources'
        WHEN source.id = 'official-event-calendar' THEN 'update_event_calendar'
        WHEN source.id LIKE 'house_%' OR source.id LIKE 'sec_13f_%'
          OR source.id LIKE 'disclosure_csv_%' OR source.id = 'sec_disclosures'
          THEN 'update_disclosures'
        WHEN source.id IN ('watchlist_quote', 'daily-market-prices', 'tradingview',
                           'yfinance_info', 'coingecko', 'yfinance') THEN 'update_market_data'
        ELSE NULL END"""


def _env_int(name: str, default: int, *, allow_zero: bool = False) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < 0:
        return default
    if value == 0:
        return 0 if allow_zero else default
    return value


def _env_int_optional(name: str) -> int | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    return value if value > 0 else 0


def _first_interval(intervals: dict[str, int], *prefixes: str) -> int:
    for prefix in prefixes:
        for job, seconds in intervals.items():
            if job.startswith(prefix):
                return seconds
    return 0


def _option_agent_config(config: Any | None) -> Any:
    if config is None:
        from investment_panel.core.config import load_config

        config = load_config()
    agents = _config_value(config, "agents", {})
    return _config_value(agents, "option_agent", {})


def _options_decision_config(config: Any | None) -> Any:
    if config is None:
        from investment_panel.core.config import load_config

        config = load_config()
    analysis = _config_value(config, "analysis", {})
    return _config_value(analysis, "options_decision_system", {})


def _config_value(source: Any, key: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(key, default)
    return getattr(source, key, default)
