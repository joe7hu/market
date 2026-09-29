from pathlib import Path
from types import SimpleNamespace

import yaml

from conftest import typed_config
from investment_panel.core.job_policy import scheduler_intervals


def test_shipped_agent_configuration_has_a_bounded_daily_proposal_producer(monkeypatch):
    raw = yaml.safe_load(Path("config.yaml").read_text())
    monkeypatch.delenv("MARKET_AGENT_REFRESH_SECONDS", raising=False)
    config = typed_config(raw=raw)
    assert config.agents.option_agent.auto_run_seconds == 86400
    assert config.agents.option_agent.max_runs_per_day == 1
    assert scheduler_intervals(config)["run_option_agents"] == 86400
    assert "run_option_paper_experiments" in scheduler_intervals(config)


def test_reference_universe_is_the_exact_same_bounded_detector_denominator():
    from investment_panel.infrastructure.postgres.recovery_universe import (
        detector_universe,
    )

    calls = []

    def discover(configured, *, limit):
        calls.append((configured, limit))
        return ["NVDA", "ASTS", "QCOM", "ARM"][:limit]

    ingestion = SimpleNamespace(option_universe=discover)
    events = SimpleNamespace(current_event_symbols=lambda **_: ["RKLB"])
    result, active = detector_universe(
        ingestion, events, configured=[{"symbol": "NVDA"}], limit=4
    )
    assert result == ["RKLB", "NVDA", "ASTS", "QCOM"]
    assert active == ["RKLB"]
    assert calls == [([{"symbol": "RKLB"}, {"symbol": "NVDA"}], 4)]


def test_crypto_assessment_recurrence_honors_utc_rollover_without_market_session_filter():
    from datetime import UTC, datetime
    from investment_panel.infrastructure import scheduler

    assert (
        scheduler._recurring_delay_seconds(
            "refresh_assessment_inputs",
            300,
            reference_time=datetime(2026, 9, 28, 23, 59, tzinfo=UTC),
        )
        == 75
    )
    assert (
        scheduler._recurring_delay_seconds(
            "refresh_assessment_inputs",
            300,
            reference_time=datetime(2026, 9, 27, 12, tzinfo=UTC),
        )
        == 300
    )
    assert (
        scheduler.DECISION_PIPELINE_SUCCESSORS["refresh_assessment_inputs"]
        == "refresh_symbol_features"
    )
