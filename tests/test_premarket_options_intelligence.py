from __future__ import annotations

from investment_panel.jobs import premarket_options_intelligence
from investment_panel.infrastructure.postgres import agents
from investment_panel.infrastructure.postgres.runtime import JOB_PROFILE


def test_premarket_intelligence_uses_postgresql_today_composition(monkeypatch) -> None:
    monkeypatch.setattr(
        premarket_options_intelligence.postgres_refresh,
        "premarket",
        lambda path: {"status": "ok", "database": "postgresql:///market", "today": {"daily_brief": 3}},
    )
    result = premarket_options_intelligence.run("config.yaml", strategy_version="v-test")
    assert result["strategy_version"] == "v-test"
    assert result["today"]["daily_brief"] == 3


def test_scheduled_agent_queue_uses_the_job_database_profile(monkeypatch) -> None:
    repository = agents.AgentRepository(object())
    calls = []
    monkeypatch.setattr(agents, "current_candidate_payloads", lambda *_args, **_kwargs: [{"ticker": "NVDA"}])
    monkeypatch.setattr(
        repository,
        "queue_thesis",
        lambda ticker, **kwargs: calls.append((ticker, kwargs)) or {"status": "queued"},
    )

    assert repository.queue_current_candidates(limit=1) == 1
    assert calls[0][0] == "NVDA"
    assert calls[0][1]["profile"] == JOB_PROFILE
